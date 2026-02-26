"""Entity boost for memory search — checks Redis for entity matches."""
import subprocess
import re

ENTITY_PATTERNS = [
    r"\b(EXAMPLE_CLIENT|YOUR_COMPANY|Your Company)\b",
    r"\b(Qdrant|PostgreSQL|Redis|Docker|Mem0|Tailscale|ngrok)\b",
    r"\b(PowerShell|Graph API|Teams|Cloud Platform|CloudPlatform)\b",
    r"\b(SIP|VoIP|WebRTC|LiveKit|Twilio|ElevenLabs|Deepgram)\b",
    r"\b(Telephony Service A|Telephony Service B|Call Routing)\b",
    r"\b(ClawVault|ClawCrashCart|ClawBoss|ClawBack|OpenClaw)\b",
    r"\b(ClawBot|OpenClaw|ClawBoss|ClawCrashCart|ClawVault)\b",
]

def get_boost(query, mem_id_prefix):
    """Return entity boost score for a memory given the search query."""
    # Extract entities from query
    found = set()
    for pat in ENTITY_PATTERNS:
        for match in re.findall(pat, query, re.IGNORECASE):
            found.add(match)
    if not found:
        return 0.0

    # Look up postgres memory UUID from qdrant point ID prefix
    try:
        r = subprocess.run(
            ["docker", "exec", "openclaw-memory-postgres-1", "psql", "-U", "openclaw",
             "-d", "openclaw_memory", "-t", "-A", "-c",
             f"SELECT id FROM memories WHERE qdrant_point_id::text LIKE '{mem_id_prefix}%' LIMIT 1"],
            capture_output=True, text=True, timeout=5)
        pg_id = r.stdout.strip()
        if not pg_id:
            return 0.0
    except Exception:
        return 0.0

    # Check Redis for entity membership
    boost = 0.0
    for entity in found:
        try:
            r = subprocess.run(
                ["docker", "exec", "openclaw-memory-redis-1", "redis-cli",
                 "SISMEMBER", f"entity:{entity}", pg_id],
                capture_output=True, text=True, timeout=3)
            if r.stdout.strip() == "1":
                boost += 0.15
        except Exception:
            pass
    return min(0.5, boost)
