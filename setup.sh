#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# ClaudeClaw Memory Stack Setup
# Installs ClawCrashKit v2 — Qdrant + PostgreSQL + Redis + CLI tools
# ═══════════════════════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GREEN="\033[92m"; RED="\033[91m"; CYAN="\033[96m"; BOLD="\033[1m"; RESET="\033[0m"

echo -e "\n${CYAN}${BOLD}═══════════════════════════════════════════════════════"
echo -e "  ClaudeClaw Memory Stack Setup (ClawCrashKit v2)"
echo -e "═══════════════════════════════════════════════════════${RESET}\n"

# ─── Check prerequisites ───────────────────────────────────────────
echo -e "${BOLD}Checking prerequisites...${RESET}"

if ! command -v docker &>/dev/null; then
    echo -e "  ${RED}✗${RESET} Docker not found. Install: https://docs.docker.com/engine/install/"
    exit 1
fi
echo -e "  ${GREEN}✓${RESET} Docker"

if ! docker compose version &>/dev/null; then
    echo -e "  ${RED}✗${RESET} Docker Compose not found"
    exit 1
fi
echo -e "  ${GREEN}✓${RESET} Docker Compose"

if ! command -v python3 &>/dev/null; then
    echo -e "  ${RED}✗${RESET} Python3 not found"
    exit 1
fi
echo -e "  ${GREEN}✓${RESET} Python3"

# ─── Install Python dependencies ──────────────────────────────────
echo -e "\n${BOLD}Installing Python dependencies...${RESET}"
pip install mem0ai cryptography --break-system-packages -q 2>/dev/null || \
pip install mem0ai cryptography -q 2>/dev/null || \
echo -e "  ${RED}⚠${RESET} pip install failed — install manually: pip install mem0ai cryptography"

# ─── Set up PostgreSQL password ───────────────────────────────────
echo -e "\n${BOLD}Configuring secrets...${RESET}"
mkdir -p "$SCRIPT_DIR/secrets"
if [ ! -f "$SCRIPT_DIR/secrets/pg_password.txt" ]; then
    PG_PASS=$(openssl rand -base64 24 2>/dev/null || python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    echo -n "$PG_PASS" > "$SCRIPT_DIR/secrets/pg_password.txt"
    chmod 600 "$SCRIPT_DIR/secrets/pg_password.txt"
    echo -e "  ${GREEN}✓${RESET} PostgreSQL password generated"
else
    echo -e "  ${GREEN}✓${RESET} PostgreSQL password already exists"
fi

# ─── Start Docker stack ───────────────────────────────────────────
echo -e "\n${BOLD}Starting memory stack...${RESET}"
cd "$SCRIPT_DIR"
docker compose up -d

echo -e "\n${BOLD}Waiting for services...${RESET}"
sleep 5

# Check health
for svc in openclaw-memory-qdrant-1 openclaw-memory-postgres-1 openclaw-memory-redis-1; do
    if docker inspect -f '{{.State.Running}}' "$svc" 2>/dev/null | grep -q true; then
        echo -e "  ${GREEN}✓${RESET} $svc"
    else
        echo -e "  ${RED}✗${RESET} $svc — check: docker logs $svc"
    fi
done

# ─── Initialize PostgreSQL schema ─────────────────────────────────
echo -e "\n${BOLD}Initializing database schema...${RESET}"
# Wait for postgres to be ready
for i in {1..10}; do
    if docker exec openclaw-memory-postgres-1 pg_isready -U openclaw -d openclaw_memory &>/dev/null; then
        break
    fi
    sleep 2
done
docker exec -i openclaw-memory-postgres-1 psql -U openclaw -d openclaw_memory < "$SCRIPT_DIR/init-db.sql" 2>/dev/null
echo -e "  ${GREEN}✓${RESET} Schema initialized"

# ─── Install CLI tools (symlinks) ─────────────────────────────────
echo -e "\n${BOLD}Installing CLI tools...${RESET}"
ln -sf "$SCRIPT_DIR/openclaw_memo.py" /usr/local/bin/openclaw-memo 2>/dev/null && \
    echo -e "  ${GREEN}✓${RESET} openclaw-memo" || echo -e "  ${RED}⚠${RESET} openclaw-memo (run as root)"
ln -sf "$SCRIPT_DIR/openclaw_custodian.py" /usr/local/bin/openclaw-custodian 2>/dev/null && \
    echo -e "  ${GREEN}✓${RESET} openclaw-custodian" || echo -e "  ${RED}⚠${RESET} openclaw-custodian"
ln -sf "$SCRIPT_DIR/clawcrashcart.py" /usr/local/bin/clawcrashcart 2>/dev/null && \
    echo -e "  ${GREEN}✓${RESET} clawcrashcart" || echo -e "  ${RED}⚠${RESET} clawcrashcart"
ln -sf "$SCRIPT_DIR/session_ingest.py" /usr/local/bin/openclaw-session-ingest 2>/dev/null && \
    echo -e "  ${GREEN}✓${RESET} openclaw-session-ingest" || echo -e "  ${RED}⚠${RESET} openclaw-session-ingest"

# ─── Set up cron jobs ─────────────────────────────────────────────
echo -e "\n${BOLD}Setting up cron jobs...${RESET}"
CRON_MARKER="# ClaudeClaw Memory Stack"
if crontab -l 2>/dev/null | grep -q "$CRON_MARKER"; then
    echo -e "  ${GREEN}✓${RESET} Cron jobs already configured"
else
    (crontab -l 2>/dev/null; echo ""; echo "$CRON_MARKER"; \
     echo "*/5 * * * * $SCRIPT_DIR/memory-refresh.sh >> /tmp/memory-refresh.log 2>&1"; \
     echo "*/30 * * * * /usr/local/bin/openclaw-custodian run >> /tmp/custodian.log 2>&1"; \
     echo "*/10 * * * * /usr/local/bin/openclaw-session-ingest >> /tmp/session-ingest.log 2>&1"; \
     echo "0 3 * * * /usr/local/bin/clawcrashcart backup --notes daily >> /tmp/backup.log 2>&1") | crontab -
    echo -e "  ${GREEN}✓${RESET} Cron jobs installed"
fi

# ─── Done ─────────────────────────────────────────────────────────
echo -e "\n${CYAN}${BOLD}═══════════════════════════════════════════════════════"
echo -e "  Memory Stack Ready!"
echo -e "═══════════════════════════════════════════════════════${RESET}"
echo ""
echo "  Services:  docker compose -f $SCRIPT_DIR/docker-compose.yml ps"
echo "  Status:    openclaw-memo status"
echo "  Add:       openclaw-memo add \"your fact\" --user your_user"
echo "  Search:    openclaw-memo search \"query\" --user your_user"
echo "  Stats:     openclaw-custodian stats"
echo "  Backup:    clawcrashcart backup"
echo ""
echo -e "  ${BOLD}Note:${RESET} Configure ClawVault credentials before using openclaw-memo."
echo -e "  See: /etc/openclaw/vault.env"
echo ""
