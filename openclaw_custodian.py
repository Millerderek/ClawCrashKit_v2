#!/usr/bin/env python3
"""
openclaw-custodian — Memory Custodian Sub-Agent

Background process that maintains OpenClaw's memory stack:
  - Syncs Qdrant vectors → PostgreSQL metadata
  - Recalculates keyscores (recency decay, frequency, authority)
  - Extracts and indexes entities
  - Garbage-collects stale/expired memories
  - Updates Redis cache for fast pre-filtering
  - Logs all maintenance runs to PostgreSQL

Usage:
  openclaw-custodian run          # Full maintenance cycle
  openclaw-custodian scores       # Recalculate keyscores only
  openclaw-custodian gc           # Garbage collection only
  openclaw-custodian entities     # Re-extract entities only
  openclaw-custodian stats        # Show memory stats
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    "pg_container": "openclaw-memory-postgres-1",
    "pg_user": "openclaw",
    "pg_db": "openclaw_memory",
    "redis_container": "openclaw-memory-redis-1",
    "qdrant_url": "http://127.0.0.1:6333",
    "qdrant_collection": "openclaw_memories",
    "weight_recency": 0.35,
    "weight_frequency": 0.20,
    "weight_authority": 0.30,
    "weight_entity": 0.15,
    "recency_decay_rate": 0.05,
    "stale_days": 60,
    "gc_days": 90,
    "entity_patterns": {
        "client": [r"\b(EXAMPLE_CLIENT|YOUR_COMPANY|Your Company)\b"],
        "technology": [
            r"\b(Qdrant|PostgreSQL|Redis|Docker|Mem0|Tailscale|ngrok)\b",
            r"\b(PowerShell|Graph API|Teams|Cloud Platform|CloudPlatform)\b",
            r"\b(SIP|VoIP|WebRTC|LiveKit|Twilio|ElevenLabs|Deepgram)\b",
            r"\b(Telephony Service A|Telephony Service B|Call Routing)\b",
            r"\b(ClawVault|ClawCrashCart|ClawBoss|ClawBack|OpenClaw)\b",
            r"\b(Kimi K2\.5|Claude|Gemini|GPT-4o)\b",
        ],
        "person": [r"\b(YOUR_NAME|YOUR_AGENT)\b"],
        "project": [
            r"\b(ClawBot|OpenClaw|ClawBoss|ClawCrashCart|ClawVault)\b",
            r"\b(AI helpdesk|memory stack|voice gateway)\b",
        ],
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# Terminal output
# ═══════════════════════════════════════════════════════════════════════════════

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; CN = "\033[96m"; B = "\033[1m"; D = "\033[2m"; X = "\033[0m"
def ok(m):   print(f"  {G}✓{X} {m}")
def warn(m): print(f"  {Y}⚠{X} {m}")
def err(m):  print(f"  {R}✗{X} {m}")
def info(m): print(f"  {CN}→{X} {m}")

# ═══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════════

def pg(sql: str) -> str:
    cmd = f'docker exec {CONFIG["pg_container"]} psql -U {CONFIG["pg_user"]} -d {CONFIG["pg_db"]} -t -A -c "{sql}"'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and "ERROR" in r.stderr:
        raise RuntimeError(f"PG: {r.stderr.strip()}")
    return r.stdout.strip()

def rds(cmd: str) -> str:
    r = subprocess.run(f'docker exec {CONFIG["redis_container"]} redis-cli {cmd}',
                       shell=True, capture_output=True, text=True, timeout=10)
    return r.stdout.strip()

def qdrant_get(path: str) -> dict:
    import urllib.request
    with urllib.request.urlopen(f'{CONFIG["qdrant_url"]}{path}', timeout=10) as r:
        return json.loads(r.read())

def qdrant_post(path: str, data: dict) -> dict:
    import urllib.request
    req = urllib.request.Request(f'{CONFIG["qdrant_url"]}{path}',
                                data=json.dumps(data).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# ═══════════════════════════════════════════════════════════════════════════════
# Step 1: Sync Qdrant → PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════════

def sync_qdrant_to_postgres():
    info("Syncing Qdrant → PostgreSQL...")
    coll = CONFIG["qdrant_collection"]
    result = qdrant_post(f"/collections/{coll}/points/scroll", {
        "limit": 1000, "with_payload": True, "with_vector": False,
    })
    points = result.get("result", {}).get("points", [])
    if not points:
        warn("No points in Qdrant"); return 0

    import hashlib
    synced = 0
    for pt in points:
        pid = str(pt["id"])
        payload = pt.get("payload", {})
        text = payload.get("data", payload.get("text", payload.get("memory", "")))
        if not text: continue

        exists = pg(f"SELECT COUNT(*) FROM memories WHERE qdrant_point_id = '{pid}'")
        if exists == "0":
            safe = text[:500].replace("'", "''")
            chash = hashlib.sha256(text.encode()).hexdigest()
            pg(f"INSERT INTO memories (qdrant_point_id, collection, content_hash, summary, source, confidence) "
               f"VALUES ('{pid}', '{coll}', '{chash}', '{safe}', 'auto', 'direct') "
               f"ON CONFLICT (qdrant_point_id) DO NOTHING")
            mid = pg(f"SELECT id FROM memories WHERE qdrant_point_id = '{pid}'")
            if mid:
                pg(f"INSERT INTO keyscores (memory_id, recency_score, frequency_score, authority_score, entity_boost, composite_score) "
                   f"VALUES ('{mid}', 1.0, 0.0, 0.5, 0.0, 0.5) ON CONFLICT (memory_id) DO NOTHING")
            synced += 1

    ok(f"Synced {synced} new points ({len(points)} total in Qdrant)")
    return synced

# ═══════════════════════════════════════════════════════════════════════════════
# Step 2: Recalculate keyscores
# ═══════════════════════════════════════════════════════════════════════════════

def recalculate_keyscores():
    info("Recalculating keyscores...")
    pg("UPDATE keyscores k SET "
       "recency_score = compute_recency_score(m.last_accessed, 0.05), "
       "frequency_score = compute_frequency_score(m.access_count), "
       "computed_at = NOW() "
       "FROM memories m WHERE k.memory_id = m.id AND m.is_deprecated = FALSE")

    pg("UPDATE keyscores SET "
       "composite_score = compute_composite_score(recency_score, frequency_score, authority_score, entity_boost), "
       "computed_at = NOW()")

    count = pg("SELECT COUNT(*) FROM keyscores")
    avg = pg("SELECT ROUND(AVG(composite_score)::numeric, 3) FROM keyscores")
    ok(f"Keyscores updated: {count} memories, avg: {avg}")
    return int(count) if count else 0

# ═══════════════════════════════════════════════════════════════════════════════
# Step 3: Entity extraction
# ═══════════════════════════════════════════════════════════════════════════════

def extract_entities():
    info("Extracting entities...")
    rows = pg("SELECT id, summary FROM memories WHERE is_deprecated = FALSE")
    if not rows:
        warn("No memories to process"); return 0

    total = 0
    for row in rows.split("\n"):
        if "|" not in row: continue
        mid, text = row.split("|", 1)
        mid, text = mid.strip(), text.strip()
        if not mid or not text: continue

        for etype, patterns in CONFIG["entity_patterns"].items():
            for pat in patterns:
                for match in set(re.findall(pat, text, re.IGNORECASE)):
                    safe = match.replace("'", "''")
                    pg(f"INSERT INTO memory_entities (memory_id, entity_name, entity_type) "
                       f"VALUES ('{mid}', '{safe}', '{etype}') ON CONFLICT (memory_id, entity_name) DO NOTHING")
                    total += 1

    count = pg("SELECT COUNT(DISTINCT entity_name) FROM memory_entities")
    ok(f"Entities: {count} unique across {total} tags")
    return total

# ═══════════════════════════════════════════════════════════════════════════════
# Step 4: Garbage collection
# ═══════════════════════════════════════════════════════════════════════════════

def garbage_collect():
    info("Running garbage collection...")
    gc_d, stale_d = CONFIG["gc_days"], CONFIG["stale_days"]

    total = pg("SELECT COUNT(*) FROM memories WHERE is_deprecated = FALSE")
    expired = pg("SELECT COUNT(*) FROM memories WHERE is_deprecated = FALSE AND ttl_expires IS NOT NULL AND ttl_expires < NOW()")

    if expired and int(expired) > 0:
        pg("UPDATE memories SET is_deprecated = TRUE, deprecated_reason = 'ttl_expired' "
           "WHERE ttl_expires IS NOT NULL AND ttl_expires < NOW() AND is_deprecated = FALSE")
        ok(f"Deprecated {expired} TTL-expired")

    very_old = pg(
        f"SELECT COUNT(*) FROM memories WHERE is_deprecated = FALSE "
        f"AND (last_accessed < NOW() - INTERVAL '{gc_d} days' OR "
        f"(last_accessed IS NULL AND created_at < NOW() - INTERVAL '{gc_d} days')) "
        f"AND access_count < 2")

    if very_old and int(very_old) > 0:
        pg(f"UPDATE memories SET is_deprecated = TRUE, deprecated_reason = 'stale' "
           f"WHERE is_deprecated = FALSE "
           f"AND (last_accessed < NOW() - INTERVAL '{gc_d} days' OR "
           f"(last_accessed IS NULL AND created_at < NOW() - INTERVAL '{gc_d} days')) "
           f"AND access_count < 2")
        ok(f"Deprecated {very_old} stale (>{gc_d}d, <2 accesses)")

    ok(f"GC: {total} active, {expired} expired")
    return int(total) if total else 0

# ═══════════════════════════════════════════════════════════════════════════════
# Step 5: Contradiction detection
# ═══════════════════════════════════════════════════════════════════════════════

# Heuristic contradiction signals — patterns that indicate conflicting facts
CONTRADICTION_SIGNALS = [
    # Negation pairs
    (r"\bnot\b", r"\b(?:is|are|was|were|has|have|does|do|will|can)\b"),
    # Status changes
    (r"\b(?:enabled|active|running|installed)\b", r"\b(?:disabled|inactive|stopped|removed)\b"),
    # Location/value changes (same entity, different values)
    (r"\bport\s+(\d+)", r"\bport\s+(\d+)"),
    (r"\b(?:at|on|in)\s+([/\w.-]+)", r"\b(?:at|on|in)\s+([/\w.-]+)"),
    # Role/org changes
    (r"\b(?:works at|employed by|works for)\s+(\w+)", r"\b(?:works at|employed by|works for)\s+(\w+)"),
]

def detect_contradictions():
    """Find memories sharing entities that may contradict each other."""
    info("Scanning for contradictions...")

    # Get entity→memories mapping (only active memories)
    rows = pg(
        "SELECT e.entity_name, array_agg(DISTINCT e.memory_id::text) as mem_ids "
        "FROM memory_entities e "
        "JOIN memories m ON e.memory_id = m.id "
        "WHERE m.is_deprecated = FALSE "
        "GROUP BY e.entity_name "
        "HAVING COUNT(DISTINCT e.memory_id) >= 2"
    )

    if not rows:
        ok("No entity groups to check")
        return 0

    # Build pairs to compare
    pairs_to_check = set()
    for row in rows.split("\n"):
        if "|" not in row: continue
        entity, mem_ids_raw = [x.strip() for x in row.split("|", 1)]
        # Parse the PostgreSQL array format {uuid1,uuid2,...}
        mem_ids = [x.strip() for x in mem_ids_raw.strip("{}").split(",") if x.strip()]
        # Generate all pairs
        for i in range(len(mem_ids)):
            for j in range(i + 1, len(mem_ids)):
                a, b = sorted([mem_ids[i], mem_ids[j]])
                pairs_to_check.add((a, b))

    info(f"Checking {len(pairs_to_check)} memory pairs...")

    # Check each pair for contradictions
    new_contradictions = 0
    for mem_a, mem_b in pairs_to_check:
        # Skip already-flagged pairs
        existing = pg(
            f"SELECT COUNT(*) FROM contradictions "
            f"WHERE memory_a_id = '{mem_a}' AND memory_b_id = '{mem_b}'"
        )
        if existing != "0":
            continue

        # Get memory texts
        text_a = pg(f"SELECT summary FROM memories WHERE id = '{mem_a}'")
        text_b = pg(f"SELECT summary FROM memories WHERE id = '{mem_b}'")

        if not text_a or not text_b:
            continue

        # Heuristic contradiction check
        score = _heuristic_contradiction_score(text_a, text_b)

        if score >= 0.75:
            # Flag as potential contradiction
            safe_a = mem_a
            safe_b = mem_b
            pg(
                f"INSERT INTO contradictions (memory_a_id, memory_b_id) "
                f"VALUES ('{safe_a}', '{safe_b}') "
                f"ON CONFLICT (memory_a_id, memory_b_id) DO NOTHING"
            )
            new_contradictions += 1
            warn(f"Contradiction: [{text_a[:60]}...] vs [{text_b[:60]}...]")
            # Notify via Telegram
            try:
                import sys; sys.path.insert(0, "/root/openclaw-memory")
                from telegram_notify import send
                send(f"⚡ *Contradiction Detected*\n\nMemory A: {text_a[:100]}\n\nMemory B: {text_b[:100]}\n\nResolve with:\n`openclaw-custodian feedback {mem_a[:8]} correct`\nor\n`openclaw-custodian feedback {mem_b[:8]} incorrect`", parse_mode="Markdown")
            except Exception:
                pass

    total = pg("SELECT COUNT(*) FROM contradictions WHERE resolved = FALSE")
    ok(f"Contradictions: {new_contradictions} new, {total} total unresolved")
    return new_contradictions


def _heuristic_contradiction_score(text_a: str, text_b: str) -> float:
    """
    Score how likely two memories contradict each other (0.0 = no conflict, 1.0 = definite conflict).
    Uses pattern matching and simple heuristics.
    """
    score = 0.0
    ta, tb = text_a.lower(), text_b.lower()

    # Check for negation contradictions
    negation_words = ["not", "no longer", "don't", "doesn't", "isn't", "wasn't", "never", "stopped", "removed", "disabled"]
    for neg in negation_words:
        if neg in ta and neg not in tb:
            # Find what's being negated — check if the other memory asserts it
            # Simple: check word overlap excluding common words
            words_a = set(ta.split()) - {"the", "a", "an", "is", "are", "was", "were", "to", "for", "of", "in", "on", "at", "and", "or"}
            words_b = set(tb.split()) - {"the", "a", "an", "is", "are", "was", "were", "to", "for", "of", "in", "on", "at", "and", "or"}
            overlap = words_a & words_b
            if len(overlap) >= 3:
                score += 0.4
                break

    # Check for status contradictions
    status_pairs = [
        ({"enabled", "active", "running", "installed", "live"},
         {"disabled", "inactive", "stopped", "removed", "dead", "down"}),
        ({"true", "yes"}, {"false", "no"}),
    ]
    for positive, negative in status_pairs:
        a_pos = bool(positive & set(ta.split()))
        a_neg = bool(negative & set(ta.split()))
        b_pos = bool(positive & set(tb.split()))
        b_neg = bool(negative & set(tb.split()))
        if (a_pos and b_neg) or (a_neg and b_pos):
            score += 0.3

    # Check for different values for same attribute (ports, paths, IPs)
    port_pattern = r"port\s*[:=]?\s*(\d+)"
    ports_a = set(re.findall(port_pattern, ta))
    ports_b = set(re.findall(port_pattern, tb))
    if ports_a and ports_b and ports_a != ports_b:
        # Same concept (port), different values
        words_a = set(ta.split())
        words_b = set(tb.split())
        if len(words_a & words_b) >= 3:
            score += 0.5

    path_pattern = r"(/[\w/.=-]+)"
    paths_a = set(re.findall(path_pattern, ta))
    paths_b = set(re.findall(path_pattern, tb))
    if paths_a and paths_b:
        # Check if they refer to the same thing but different paths
        common_words = set(ta.split()) & set(tb.split())
        if len(common_words) >= 4 and paths_a != paths_b:
            score += 0.3

    # Check for "works at X" vs "works at Y" with different X/Y
    org_pattern = r"(?:works at|employed by|at|from)\s+(\w+)"
    orgs_a = set(re.findall(org_pattern, ta))
    orgs_b = set(re.findall(org_pattern, tb))
    if orgs_a and orgs_b and orgs_a != orgs_b:
        overlap = set(ta.split()) & set(tb.split())
        if len(overlap) >= 3:
            score += 0.4

    return min(1.0, score)

# ═══════════════════════════════════════════════════════════════════════════════
# Step 6: Redis cache
# ═══════════════════════════════════════════════════════════════════════════════

def update_redis_cache():
    info("Updating Redis cache...")
    rows = pg("SELECT entity_name, memory_id FROM memory_entities")
    if not rows:
        warn("No entity mappings"); return 0

    emap = {}
    for row in rows.split("\n"):
        if "|" not in row: continue
        e, m = [x.strip() for x in row.split("|")]
        if e and m: emap.setdefault(e, []).append(m)

    for entity, mids in emap.items():
        key = f"entity:{entity}"
        rds(f"DEL {key}")
        for mid in mids:
            rds(f"SADD {key} {mid}")

    scores = pg(
        "SELECT m.qdrant_point_id, k.composite_score "
        "FROM keyscores k JOIN memories m ON k.memory_id = m.id "
        "WHERE m.is_deprecated = FALSE ORDER BY k.composite_score DESC LIMIT 50")

    sc = 0
    for row in (scores or "").split("\n"):
        if "|" not in row: continue
        pid, score = [x.strip() for x in row.split("|")]
        if pid and score:
            rds(f"SET keyscore:{pid} {score} EX 600")
            sc += 1

    ok(f"Redis: {len(emap)} entity sets, {sc} keyscores cached")
    return len(emap)

# ═══════════════════════════════════════════════════════════════════════════════
# Job logging
# ═══════════════════════════════════════════════════════════════════════════════

def log_job(agent: str, jtype: str, status: str, details: dict = None, affected: int = 0):
    try:
        if details:
            d = json.dumps(details)
            cmd = ["docker", "exec", "openclaw-memory-postgres-1", "psql", "-U", "openclaw", "-d", "openclaw_memory", "-c",
                   f"INSERT INTO agent_jobs (agent_name, job_type, finished_at, status, details, memories_affected) VALUES ('{agent}', '{jtype}', NOW(), '{status}', '{d}'::jsonb, {affected})"]
            subprocess.run(cmd, capture_output=True, timeout=10)
        else:
            pg(f"INSERT INTO agent_jobs (agent_name, job_type, finished_at, status, memories_affected) VALUES ('{agent}', '{jtype}', NOW(), '{status}', {affected})")
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_run(args):
    print(f"\n{CN}{B}{'═' * 50}\n  OpenClaw Custodian — Full Cycle\n{'═' * 50}{X}\n")
    start = time.time()
    results = {}
    try:
        results["sync"] = sync_qdrant_to_postgres()
        results["entities"] = extract_entities()
        results["scores"] = recalculate_keyscores()
        results["gc"] = garbage_collect()
        results["contradictions"] = detect_contradictions()
        results["cache"] = update_redis_cache()
        elapsed = time.time() - start
        log_job("custodian", "full_cycle", "completed", results, results.get("scores", 0))
        print(f"\n  {G}✓ {B}Custodian cycle complete in {elapsed:.1f}s{X}")
    except Exception as e:
        err(f"Failed: {e}")
        log_job("custodian", "full_cycle", "failed", {"error": str(e)})
        return 1
    return 0

def cmd_scores(args):
    print(f"\n{CN}{B}Custodian — Keyscores{X}\n")
    sync_qdrant_to_postgres()
    n = recalculate_keyscores()
    log_job("custodian", "keyscores", "completed", affected=n)

def cmd_gc(args):
    print(f"\n{CN}{B}Custodian — GC{X}\n")
    n = garbage_collect()
    log_job("custodian", "gc", "completed", affected=n)

def cmd_entities(args):
    print(f"\n{CN}{B}Custodian — Entities{X}\n")
    sync_qdrant_to_postgres()
    n = extract_entities()
    log_job("custodian", "entities", "completed", affected=n)

def cmd_contradictions(args):
    print(f"\n{CN}{B}Custodian — Contradiction Scan{X}\n")
    n = detect_contradictions()
    log_job("custodian", "contradictions", "completed", affected=n)

# ═══════════════════════════════════════════════════════════════════════════════
# Feedback loop
# ═══════════════════════════════════════════════════════════════════════════════

AUTHORITY_ADJUSTMENTS = {
    "correct":    +0.15,   # Boost — user confirmed this is right
    "useful":     +0.10,   # Mild boost — memory was helpful
    "irrelevant": -0.05,   # Mild penalty — not wrong, just not useful
    "incorrect":  -0.25,   # Major penalty — memory is wrong
}

def apply_feedback(memory_id: str, feedback: str, context: str = ""):
    """Apply user feedback to a memory's authority score."""
    if feedback not in AUTHORITY_ADJUSTMENTS:
        err(f"Invalid feedback: {feedback}. Use: correct, incorrect, irrelevant, useful")
        return False

    adjustment = AUTHORITY_ADJUSTMENTS[feedback]

    # Record feedback
    safe_ctx = (context or "cli").replace("'", "''")
    pg(f"INSERT INTO memory_feedback (memory_id, feedback, context) "
       f"VALUES ('{memory_id}', '{feedback}', '{safe_ctx}')")

    # Adjust authority score (clamped 0.0-1.0)
    pg(f"UPDATE keyscores SET "
       f"authority_score = GREATEST(0.0, LEAST(1.0, authority_score + {adjustment})), "
       f"computed_at = NOW() "
       f"WHERE memory_id = '{memory_id}'")

    # Recompute composite
    pg(f"UPDATE keyscores SET "
       f"composite_score = compute_composite_score(recency_score, frequency_score, authority_score, entity_boost) "
       f"WHERE memory_id = '{memory_id}'")

    # If incorrect, also bump access count on the memory to track engagement
    if feedback == "incorrect":
        pg(f"UPDATE memories SET confidence = 'ambient' WHERE id = '{memory_id}' AND confidence != 'confirmed'")

    # If confirmed correct, upgrade confidence
    if feedback == "correct":
        pg(f"UPDATE memories SET confidence = 'confirmed' WHERE id = '{memory_id}'")

    return True


def cmd_feedback(args):
    """Apply feedback to a memory."""
    print(f"\n{CN}{B}Custodian — Feedback{X}\n")

    # If memory_id looks like a short prefix, try to resolve it
    mem_id = args.memory_id
    if len(mem_id) < 36:
        # Search by qdrant point ID prefix or postgres ID prefix
        full_id = pg(f"SELECT id FROM memories WHERE id::text LIKE '{mem_id}%' OR qdrant_point_id LIKE '{mem_id}%' LIMIT 1")
        if not full_id:
            err(f"No memory found matching: {mem_id}")
            return 1
        mem_id = full_id

    # Get current state
    summary = pg(f"SELECT summary FROM memories WHERE id = '{mem_id}'")
    if not summary:
        err(f"Memory not found: {mem_id}")
        return 1

    old_score = pg(f"SELECT authority_score FROM keyscores WHERE memory_id = '{mem_id}'")

    if apply_feedback(mem_id, args.feedback, args.context or ""):
        new_score = pg(f"SELECT authority_score FROM keyscores WHERE memory_id = '{mem_id}'")
        composite = pg(f"SELECT composite_score FROM keyscores WHERE memory_id = '{mem_id}'")
        adj = AUTHORITY_ADJUSTMENTS[args.feedback]
        direction = "↑" if adj > 0 else "↓"

        ok(f"Feedback applied: {args.feedback} {direction}")
        info(f"Memory: {summary[:80]}...")
        info(f"Authority: {old_score} → {new_score} ({'+' if adj > 0 else ''}{adj})")
        info(f"Composite: {composite}")
        log_job("custodian", "feedback", "completed", affected=1)
    return 0


def cmd_review(args):
    """Show memories needing review (stale, low-confidence, contradicted)."""
    print(f"\n{CN}{B}{'═' * 50}\n  Memories Needing Review\n{'═' * 50}{X}\n")

    rows = pg(
        "SELECT m.id, m.summary, m.confidence, k.composite_score, k.authority_score "
        "FROM memories m "
        "LEFT JOIN keyscores k ON k.memory_id = m.id "
        "WHERE m.is_deprecated = FALSE "
        "AND (m.confidence = 'ambient' OR k.authority_score < 0.3) "
        "ORDER BY k.authority_score ASC NULLS FIRST "
        "LIMIT 20"
    )

    if not rows or rows == "":
        ok("No memories need review")
        return 0

    count = 0
    for row in rows.split("\n"):
        if "|" not in row: continue
        parts = [x.strip() for x in row.split("|")]
        if len(parts) < 5: continue
        mid, summary, confidence, composite, authority = parts
        short_id = mid[:8]
        print(f"  {Y}•{X} [{short_id}] {summary[:70]}...")
        print(f"    confidence={confidence}  authority={authority}  composite={composite}")
        print(f"    → openclaw-custodian feedback {short_id} correct|incorrect|useful|irrelevant")
        print()
        count += 1

    info(f"{count} memories need attention")

    # Also show unresolved contradictions
    contras = pg(
        "SELECT c.id, ma.summary, mb.summary "
        "FROM contradictions c "
        "JOIN memories ma ON c.memory_a_id = ma.id "
        "JOIN memories mb ON c.memory_b_id = mb.id "
        "WHERE c.resolved = FALSE "
        "LIMIT 10"
    )

    if contras and contras != "":
        print(f"\n  {R}{B}Unresolved Contradictions:{X}\n")
        for row in contras.split("\n"):
            if "|" not in row: continue
            parts = [x.strip() for x in row.split("|")]
            if len(parts) < 3: continue
            cid, sum_a, sum_b = parts
            print(f"  {R}⚡{X} Contradiction #{cid}:")
            print(f"    A: {sum_a[:60]}...")
            print(f"    B: {sum_b[:60]}...")
            print()

    return 0

def cmd_stats(args):
    print(f"\n{CN}{B}{'═' * 50}\n  OpenClaw Memory Stats\n{'═' * 50}{X}\n")
    try:
        colls = qdrant_get("/collections")
        for c in colls.get("result", {}).get("collections", []):
            d = qdrant_get(f"/collections/{c['name']}")
            print(f"  {B}Qdrant:{X} {c['name']} — {d.get('result',{}).get('points_count',0)} vectors")
    except Exception as e: err(f"Qdrant: {e}")

    try:
        total = pg("SELECT COUNT(*) FROM memories WHERE is_deprecated = FALSE")
        dep = pg("SELECT COUNT(*) FROM memories WHERE is_deprecated = TRUE")
        ent = pg("SELECT COUNT(DISTINCT entity_name) FROM memory_entities")
        avg = pg("SELECT ROUND(AVG(composite_score)::numeric, 3) FROM keyscores")
        contra = pg("SELECT COUNT(*) FROM contradictions WHERE resolved = FALSE")
        jobs = pg("SELECT COUNT(*) FROM agent_jobs")
        print(f"  {B}Postgres:{X}")
        print(f"    Active memories:     {total}")
        print(f"    Deprecated:          {dep}")
        print(f"    Unique entities:     {ent}")
        print(f"    Avg keyscore:        {avg}")
        print(f"    Open contradictions: {contra}")
        print(f"    Custodian jobs:      {jobs}")
    except Exception as e: err(f"Postgres: {e}")

    try:
        ek = rds("KEYS 'entity:*'")
        sk = rds("KEYS 'keyscore:*'")
        print(f"  {B}Redis:{X}")
        print(f"    Entity sets:         {len(ek.split(chr(10))) if ek else 0}")
        print(f"    Cached keyscores:    {len(sk.split(chr(10))) if sk else 0}")
    except Exception as e: err(f"Redis: {e}")
    print()

def main():
    p = argparse.ArgumentParser(prog="openclaw-custodian", description="Memory Custodian")
    s = p.add_subparsers(dest="command")
    s.add_parser("run", help="Full maintenance cycle")
    s.add_parser("scores", help="Recalculate keyscores")
    s.add_parser("gc", help="Garbage collection")
    s.add_parser("entities", help="Entity extraction")
    s.add_parser("contradictions", help="Scan for contradictions")
    s.add_parser("stats", help="Memory stats")
    s.add_parser("review", help="Show memories needing review")
    p_fb = s.add_parser("feedback", help="Apply feedback to a memory")
    p_fb.add_argument("memory_id", help="Memory ID (full UUID or prefix)")
    p_fb.add_argument("feedback", choices=["correct", "incorrect", "irrelevant", "useful"], help="Feedback type")
    p_fb.add_argument("--context", default="", help="Optional context for the feedback")
    args = p.parse_args()
    if not args.command: p.print_help(); return 1
    return {"run": cmd_run, "scores": cmd_scores, "gc": cmd_gc, "entities": cmd_entities, "contradictions": cmd_contradictions, "stats": cmd_stats, "feedback": cmd_feedback, "review": cmd_review}[args.command](args)

if __name__ == "__main__":
    sys.exit(main())
