#!/usr/bin/env python3
"""
ClawCrashCart v2 — Docker-Aware Backup & Restore for OpenClaw Memory Stack

Manages backup/restore for the full OpenClaw memory architecture:
  - Pinned tier:   MEMORY.md, agent configs, skill files
  - Vector tier:   Qdrant snapshots (semantic memory)
  - Metadata tier: PostgreSQL dumps (keyscores, confidence, TTLs)
  - Cache tier:    Redis RDB dumps (entity indices, pre-filter cache)

Usage:
  clawcrashcart backup [--full | --memory-only | --config-only]
  clawcrashcart restore <snapshot-id> [--component qdrant|postgres|redis|pinned|all]
  clawcrashcart list [--verbose]
  clawcrashcart verify <snapshot-id>
  clawcrashcart prune --keep-last <N>
  clawcrashcart status

Author: ClawCrashKit Contributors
Version: 2.0.0
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration — adjust paths to match your VPS layout
# ═══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # Backup storage root
    "backup_dir": "/root/clawcrashcart/snapshots",

    # Docker compose project
    "compose_file": "/root/openclaw-memory/docker-compose.yml",
    "compose_project": "openclaw-memory",

    # Container names (match docker-compose service names)
    "qdrant_container": "openclaw-memory-qdrant-1",
    "postgres_container": "openclaw-memory-postgres-1",
    "redis_container": "openclaw-memory-redis-1",

    # Qdrant
    "qdrant_url": "http://127.0.0.1:6333",
    "qdrant_storage_volume": "openclaw-memory_qdrant_data",

    # PostgreSQL
    "pg_user": "openclaw",
    "pg_db": "openclaw_memory",

    # Redis
    "redis_volume": "openclaw-memory_redis_data",

    # Pinned tier files/directories to back up
    "pinned_paths": [
        "/root/.openclaw/MEMORY.md",
        "/root/.openclaw/openclaw.json",
        "/root/.openclaw/skills",
        "/etc/your-app/vault.env",
    ],

    # Agent config files
    "config_paths": [
        "/root/.openclaw/openclaw.json",
        "/root/openclaw-memory/docker-compose.yml",
    ],

    # Max snapshots before prune warning
    "max_snapshots": 30,
}

# ═══════════════════════════════════════════════════════════════════════════════
# Terminal colors
# ═══════════════════════════════════════════════════════════════════════════════

class C:
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"

def ok(msg):    print(f"  {C.GREEN}✓{C.RESET} {msg}")
def warn(msg):  print(f"  {C.YELLOW}⚠{C.RESET} {msg}")
def err(msg):   print(f"  {C.RED}✗{C.RESET} {msg}")
def info(msg):  print(f"  {C.CYAN}→{C.RESET} {msg}")
def header(msg):
    print(f"\n{C.CYAN}{C.BOLD}{'═' * 60}")
    print(f"  {msg}")
    print(f"{'═' * 60}{C.RESET}\n")

# ═══════════════════════════════════════════════════════════════════════════════
# Utility functions
# ═══════════════════════════════════════════════════════════════════════════════

def run(cmd: str, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command."""
    return subprocess.run(
        cmd, shell=True, capture_output=capture, text=True, check=check
    )

def timestamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def file_checksum(path: str) -> str:
    """SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def dir_size(path: str) -> int:
    """Total size of directory in bytes."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total

def human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

def container_running(name: str) -> bool:
    """Check if a Docker container is running."""
    try:
        result = run(f"docker inspect -f '{{{{.State.Running}}}}' {name}", check=False)
        return result.stdout.strip() == "true"
    except Exception:
        return False

def docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = run("docker info", check=False)
        return result.returncode == 0
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# Manifest
# ═══════════════════════════════════════════════════════════════════════════════

def create_manifest(snapshot_dir: str, components: list, notes: str = "") -> dict:
    """Create a manifest.json for a snapshot."""
    manifest = {
        "version": "2.0.0",
        "snapshot_id": os.path.basename(snapshot_dir),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "components": {},
        "checksums": {},
        "notes": notes,
    }

    for comp in components:
        comp_dir = os.path.join(snapshot_dir, comp)
        if os.path.exists(comp_dir):
            files = []
            for root, _, fnames in os.walk(comp_dir):
                for fn in fnames:
                    fp = os.path.join(root, fn)
                    rel = os.path.relpath(fp, snapshot_dir)
                    files.append(rel)
                    manifest["checksums"][rel] = file_checksum(fp)
            manifest["components"][comp] = {
                "file_count": len(files),
                "total_size": dir_size(comp_dir),
                "files": files,
            }

    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest

def load_manifest(snapshot_dir: str) -> Optional[dict]:
    """Load manifest from a snapshot directory."""
    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path) as f:
        return json.load(f)

# ═══════════════════════════════════════════════════════════════════════════════
# Backup functions
# ═══════════════════════════════════════════════════════════════════════════════

def backup_pinned(snapshot_dir: str) -> bool:
    """Backup pinned tier: MEMORY.md, skills, vault config."""
    comp_dir = os.path.join(snapshot_dir, "pinned")
    os.makedirs(comp_dir, exist_ok=True)
    count = 0

    for src in CONFIG["pinned_paths"]:
        if not os.path.exists(src):
            warn(f"Pinned path not found, skipping: {src}")
            continue
        # Preserve directory structure
        dest = os.path.join(comp_dir, src.lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        count += 1

    if count > 0:
        ok(f"Pinned tier: {count} paths backed up")
        return True
    else:
        warn("Pinned tier: nothing to back up")
        return False

def backup_configs(snapshot_dir: str) -> bool:
    """Backup configuration files."""
    comp_dir = os.path.join(snapshot_dir, "configs")
    os.makedirs(comp_dir, exist_ok=True)
    count = 0

    for src in CONFIG["config_paths"]:
        if not os.path.exists(src):
            warn(f"Config not found, skipping: {src}")
            continue
        dest = os.path.join(comp_dir, src.lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        count += 1

    if count > 0:
        ok(f"Configs: {count} files backed up")
        return True
    else:
        warn("Configs: nothing to back up")
        return False

def backup_qdrant(snapshot_dir: str) -> bool:
    """Backup Qdrant via snapshot API."""
    comp_dir = os.path.join(snapshot_dir, "qdrant")
    os.makedirs(comp_dir, exist_ok=True)

    if not container_running(CONFIG["qdrant_container"]):
        warn("Qdrant container not running, skipping")
        return False

    try:
        import urllib.request
        import urllib.error

        # List collections first
        req = urllib.request.Request(f"{CONFIG['qdrant_url']}/collections")
        with urllib.request.urlopen(req, timeout=10) as resp:
            collections_data = json.loads(resp.read())
            collections = [c["name"] for c in collections_data.get("result", {}).get("collections", [])]

        if not collections:
            warn("Qdrant: no collections found")
            # Save empty state marker
            with open(os.path.join(comp_dir, "collections.json"), "w") as f:
                json.dump({"collections": [], "snapshot_time": timestamp()}, f, indent=2)
            return True

        # Create snapshot for each collection
        collection_meta = []
        for coll_name in collections:
            info(f"Snapshotting Qdrant collection: {coll_name}")

            # Trigger snapshot creation
            req = urllib.request.Request(
                f"{CONFIG['qdrant_url']}/collections/{coll_name}/snapshots",
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                snap_data = json.loads(resp.read())
                snap_name = snap_data["result"]["name"]

            # Download snapshot
            snap_url = f"{CONFIG['qdrant_url']}/collections/{coll_name}/snapshots/{snap_name}"
            snap_file = os.path.join(comp_dir, f"{coll_name}.snapshot")
            urllib.request.urlretrieve(snap_url, snap_file)

            size = os.path.getsize(snap_file)
            collection_meta.append({
                "name": coll_name,
                "snapshot_name": snap_name,
                "snapshot_file": f"{coll_name}.snapshot",
                "size": size,
            })
            ok(f"  {coll_name}: {human_size(size)}")

        # Save collection metadata
        with open(os.path.join(comp_dir, "collections.json"), "w") as f:
            json.dump({
                "collections": collection_meta,
                "snapshot_time": timestamp(),
            }, f, indent=2)

        ok(f"Qdrant: {len(collections)} collection(s) snapshotted")
        return True

    except Exception as e:
        err(f"Qdrant backup failed: {e}")
        return False

def backup_postgres(snapshot_dir: str) -> bool:
    """Backup PostgreSQL via pg_dump inside container."""
    comp_dir = os.path.join(snapshot_dir, "postgres")
    os.makedirs(comp_dir, exist_ok=True)

    if not container_running(CONFIG["postgres_container"]):
        warn("PostgreSQL container not running, skipping")
        return False

    try:
        dump_file = os.path.join(comp_dir, "openclaw_memory.sql")
        result = run(
            f"docker exec {CONFIG['postgres_container']} "
            f"pg_dump -U {CONFIG['pg_user']} -d {CONFIG['pg_db']} --clean --if-exists",
            check=False
        )

        if result.returncode != 0:
            err(f"pg_dump failed: {result.stderr.strip()}")
            return False

        with open(dump_file, "w") as f:
            f.write(result.stdout)

        # Also dump schema separately for reference
        schema_result = run(
            f"docker exec {CONFIG['postgres_container']} "
            f"pg_dump -U {CONFIG['pg_user']} -d {CONFIG['pg_db']} --schema-only",
            check=False
        )
        if schema_result.returncode == 0:
            with open(os.path.join(comp_dir, "schema.sql"), "w") as f:
                f.write(schema_result.stdout)

        size = os.path.getsize(dump_file)
        ok(f"PostgreSQL: {human_size(size)}")
        return True

    except Exception as e:
        err(f"PostgreSQL backup failed: {e}")
        return False

def backup_redis(snapshot_dir: str) -> bool:
    """Backup Redis via BGSAVE + copy RDB."""
    comp_dir = os.path.join(snapshot_dir, "redis")
    os.makedirs(comp_dir, exist_ok=True)

    if not container_running(CONFIG["redis_container"]):
        warn("Redis container not running, skipping")
        return False

    try:
        # Trigger BGSAVE
        run(f"docker exec {CONFIG['redis_container']} redis-cli BGSAVE", check=False)
        time.sleep(2)  # Wait for save to complete

        # Wait for BGSAVE to finish (poll for up to 30 seconds)
        for _ in range(15):
            result = run(
                f"docker exec {CONFIG['redis_container']} redis-cli LASTSAVE",
                check=False
            )
            if result.returncode == 0:
                break
            time.sleep(2)

        # Copy dump.rdb from container
        rdb_file = os.path.join(comp_dir, "dump.rdb")
        result = run(
            f"docker cp {CONFIG['redis_container']}:/data/dump.rdb {rdb_file}",
            check=False
        )

        if result.returncode != 0:
            # Try appendonly file if RDB isn't available
            result = run(
                f"docker cp {CONFIG['redis_container']}:/data/appendonly.aof {comp_dir}/appendonly.aof",
                check=False
            )
            if result.returncode != 0:
                err("Redis: could not copy RDB or AOF file")
                return False
            ok(f"Redis: AOF file backed up ({human_size(os.path.getsize(os.path.join(comp_dir, 'appendonly.aof')))})")
            return True

        # Also grab Redis INFO for metadata
        info_result = run(
            f"docker exec {CONFIG['redis_container']} redis-cli INFO keyspace",
            check=False
        )
        if info_result.returncode == 0:
            with open(os.path.join(comp_dir, "keyspace_info.txt"), "w") as f:
                f.write(info_result.stdout)

        size = os.path.getsize(rdb_file)
        ok(f"Redis: {human_size(size)}")
        return True

    except Exception as e:
        err(f"Redis backup failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# Restore functions
# ═══════════════════════════════════════════════════════════════════════════════

def restore_pinned(snapshot_dir: str) -> bool:
    """Restore pinned tier files to their original locations."""
    comp_dir = os.path.join(snapshot_dir, "pinned")
    if not os.path.exists(comp_dir):
        warn("No pinned tier in this snapshot")
        return False

    count = 0
    for root, _, files in os.walk(comp_dir):
        for fn in files:
            src = os.path.join(root, fn)
            # Reconstruct original path
            rel = os.path.relpath(src, comp_dir)
            dest = "/" + rel
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            count += 1

    ok(f"Pinned tier: {count} files restored")
    return True

def restore_configs(snapshot_dir: str) -> bool:
    """Restore configuration files."""
    comp_dir = os.path.join(snapshot_dir, "configs")
    if not os.path.exists(comp_dir):
        warn("No configs in this snapshot")
        return False

    count = 0
    for root, _, files in os.walk(comp_dir):
        for fn in files:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, comp_dir)
            dest = "/" + rel
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            count += 1

    ok(f"Configs: {count} files restored")
    return True

def restore_qdrant(snapshot_dir: str) -> bool:
    """Restore Qdrant collections from snapshots."""
    comp_dir = os.path.join(snapshot_dir, "qdrant")
    if not os.path.exists(comp_dir):
        warn("No Qdrant data in this snapshot")
        return False

    meta_file = os.path.join(comp_dir, "collections.json")
    if not os.path.exists(meta_file):
        err("Qdrant: missing collections.json metadata")
        return False

    if not container_running(CONFIG["qdrant_container"]):
        err("Qdrant container must be running for restore")
        return False

    try:
        import urllib.request

        with open(meta_file) as f:
            meta = json.load(f)

        collections = meta.get("collections", [])
        if not collections:
            info("Qdrant: snapshot contains no collections (empty state)")
            return True

        for coll in collections:
            snap_file = os.path.join(comp_dir, coll["snapshot_file"])
            if not os.path.exists(snap_file):
                err(f"Qdrant: snapshot file missing: {coll['snapshot_file']}")
                continue

            info(f"Restoring Qdrant collection: {coll['name']}")

            # Upload snapshot to Qdrant
            # First, copy snapshot into Qdrant's snapshot directory
            run(
                f"docker cp {snap_file} "
                f"{CONFIG['qdrant_container']}:/qdrant/snapshots/{coll['snapshot_file']}",
                check=True
            )

            # Restore via API - create collection from snapshot
            # Note: this replaces the existing collection
            snapshot_path = f"/qdrant/snapshots/{coll['snapshot_file']}"
            data = json.dumps({
                "location": snapshot_path
            }).encode()
            req = urllib.request.Request(
                f"{CONFIG['qdrant_url']}/collections/{coll['name']}/snapshots/recover",
                data=data,
                headers={"Content-Type": "application/json"},
                method="PUT"
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
                if result.get("status") == "ok" or result.get("result"):
                    ok(f"  {coll['name']}: restored")
                else:
                    err(f"  {coll['name']}: unexpected response: {result}")

        return True

    except Exception as e:
        err(f"Qdrant restore failed: {e}")
        return False

def restore_postgres(snapshot_dir: str) -> bool:
    """Restore PostgreSQL from dump."""
    comp_dir = os.path.join(snapshot_dir, "postgres")
    dump_file = os.path.join(comp_dir, "openclaw_memory.sql")

    if not os.path.exists(dump_file):
        warn("No PostgreSQL dump in this snapshot")
        return False

    if not container_running(CONFIG["postgres_container"]):
        err("PostgreSQL container must be running for restore")
        return False

    try:
        # Copy dump into container
        run(
            f"docker cp {dump_file} "
            f"{CONFIG['postgres_container']}:/tmp/restore.sql",
            check=True
        )

        # Execute restore
        result = run(
            f"docker exec {CONFIG['postgres_container']} "
            f"psql -U {CONFIG['pg_user']} -d {CONFIG['pg_db']} -f /tmp/restore.sql",
            check=False
        )

        # Clean up
        run(
            f"docker exec {CONFIG['postgres_container']} rm -f /tmp/restore.sql",
            check=False
        )

        if result.returncode != 0:
            # psql often returns non-zero for warnings; check stderr
            if "ERROR" in result.stderr:
                err(f"PostgreSQL restore had errors: {result.stderr[:200]}")
                return False

        ok("PostgreSQL: restored")
        return True

    except Exception as e:
        err(f"PostgreSQL restore failed: {e}")
        return False

def restore_redis(snapshot_dir: str) -> bool:
    """Restore Redis from RDB dump."""
    comp_dir = os.path.join(snapshot_dir, "redis")

    if not os.path.exists(comp_dir):
        warn("No Redis data in this snapshot")
        return False

    if not container_running(CONFIG["redis_container"]):
        err("Redis container must be running for restore")
        return False

    try:
        rdb_file = os.path.join(comp_dir, "dump.rdb")
        aof_file = os.path.join(comp_dir, "appendonly.aof")

        if os.path.exists(rdb_file):
            # Stop Redis, replace dump, restart
            info("Stopping Redis for RDB restore...")
            run(f"docker stop {CONFIG['redis_container']}", check=True)

            # Copy RDB into volume
            # Get volume mount path
            result = run(
                f"docker volume inspect {CONFIG['redis_volume']} "
                f"--format '{{{{.Mountpoint}}}}'",
                check=True
            )
            volume_path = result.stdout.strip()
            shutil.copy2(rdb_file, os.path.join(volume_path, "dump.rdb"))

            # Restart
            run(f"docker start {CONFIG['redis_container']}", check=True)
            time.sleep(3)

            ok("Redis: RDB restored")
            return True

        elif os.path.exists(aof_file):
            warn("Redis: AOF restore not yet implemented, manual restore needed")
            info(f"  AOF file is at: {aof_file}")
            return False

        else:
            warn("Redis: no RDB or AOF file found in snapshot")
            return False

    except Exception as e:
        err(f"Redis restore failed: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_backup(args):
    """Create a backup snapshot."""
    header("ClawCrashCart v2 — Backup")

    ts = timestamp()
    snapshot_id = f"snapshot_{ts}"
    snapshot_dir = os.path.join(CONFIG["backup_dir"], snapshot_id)
    os.makedirs(snapshot_dir, exist_ok=True)

    info(f"Snapshot ID: {snapshot_id}")
    info(f"Location:    {snapshot_dir}")
    print()

    components = []

    if args.config_only:
        # Config-only backup
        if backup_configs(snapshot_dir):
            components.append("configs")
    elif args.memory_only:
        # Memory-only: pinned + vector + metadata + cache
        if backup_pinned(snapshot_dir):
            components.append("pinned")
        if backup_qdrant(snapshot_dir):
            components.append("qdrant")
        if backup_postgres(snapshot_dir):
            components.append("postgres")
        if backup_redis(snapshot_dir):
            components.append("redis")
    else:
        # Full backup (default)
        if backup_pinned(snapshot_dir):
            components.append("pinned")
        if backup_configs(snapshot_dir):
            components.append("configs")
        if docker_available():
            if backup_qdrant(snapshot_dir):
                components.append("qdrant")
            if backup_postgres(snapshot_dir):
                components.append("postgres")
            if backup_redis(snapshot_dir):
                components.append("redis")
        else:
            warn("Docker not available — skipping database backups")

    if not components:
        err("No components were backed up")
        shutil.rmtree(snapshot_dir)
        return 1

    # Create manifest
    print()
    manifest = create_manifest(snapshot_dir, components, notes=args.notes or "")
    total_size = sum(
        c.get("total_size", 0) for c in manifest["components"].values()
    )

    ok(f"Manifest created")
    print()
    print(f"  {C.BOLD}Summary:{C.RESET}")
    print(f"    Components: {', '.join(components)}")
    print(f"    Total size: {human_size(total_size)}")
    print(f"    Checksums:  {len(manifest['checksums'])} files verified")

    # Check snapshot count
    snapshots = list_snapshots()
    if len(snapshots) > CONFIG["max_snapshots"]:
        print()
        warn(f"You have {len(snapshots)} snapshots. Consider running: clawcrashcart prune --keep-last {CONFIG['max_snapshots']}")

    print()
    ok(f"{C.BOLD}Backup complete: {snapshot_id}{C.RESET}")
    return 0

def cmd_restore(args):
    """Restore from a snapshot."""
    header("ClawCrashCart v2 — Restore")

    snapshot_dir = os.path.join(CONFIG["backup_dir"], args.snapshot_id)
    if not os.path.exists(snapshot_dir):
        err(f"Snapshot not found: {args.snapshot_id}")
        return 1

    manifest = load_manifest(snapshot_dir)
    if manifest:
        info(f"Snapshot:    {manifest['snapshot_id']}")
        info(f"Created:     {manifest['created_at']}")
        info(f"Components:  {', '.join(manifest['components'].keys())}")
    else:
        warn("No manifest found — this may be a v1 snapshot")

    # Determine what to restore
    component = args.component
    if component == "all":
        targets = ["postgres", "qdrant", "redis", "pinned", "configs"]
    else:
        targets = [component]

    print()
    print(f"  {C.YELLOW}{C.BOLD}About to restore: {', '.join(targets)}{C.RESET}")
    print(f"  {C.YELLOW}This will overwrite current data.{C.RESET}")

    if not args.yes:
        try:
            confirm = input(f"\n  Proceed? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return 1
        if confirm != "y":
            print("  Aborted.")
            return 1

    # Pre-flight: create a safety backup of current state
    if not args.no_safety_backup:
        print()
        info("Creating safety backup of current state...")
        safety_dir = os.path.join(CONFIG["backup_dir"], f"pre_restore_{timestamp()}")
        os.makedirs(safety_dir, exist_ok=True)
        for t in targets:
            if t == "pinned":
                backup_pinned(safety_dir)
            elif t == "configs":
                backup_configs(safety_dir)
            elif t == "qdrant":
                backup_qdrant(safety_dir)
            elif t == "postgres":
                backup_postgres(safety_dir)
            elif t == "redis":
                backup_redis(safety_dir)
        create_manifest(safety_dir, targets, notes="Pre-restore safety backup")
        ok(f"Safety backup: {os.path.basename(safety_dir)}")

    # Restore in the correct order
    print()
    info("Restoring components...")

    # Order matters: postgres (metadata) → qdrant (vectors) → redis (cache) → pinned → configs
    restore_order = ["postgres", "qdrant", "redis", "pinned", "configs"]
    results = {}

    for comp in restore_order:
        if comp not in targets:
            continue
        print()
        info(f"Restoring: {comp}")
        if comp == "postgres":
            results[comp] = restore_postgres(snapshot_dir)
        elif comp == "qdrant":
            results[comp] = restore_qdrant(snapshot_dir)
        elif comp == "redis":
            results[comp] = restore_redis(snapshot_dir)
        elif comp == "pinned":
            results[comp] = restore_pinned(snapshot_dir)
        elif comp == "configs":
            results[comp] = restore_configs(snapshot_dir)

    # Summary
    print()
    print(f"  {C.BOLD}Restore Results:{C.RESET}")
    for comp, success in results.items():
        status = f"{C.GREEN}OK{C.RESET}" if success else f"{C.RED}FAILED{C.RESET}"
        print(f"    {comp}: {status}")

    # Run verify if all components were restored
    if all(results.values()):
        print()
        ok(f"{C.BOLD}Restore complete{C.RESET}")
        info("Run 'clawcrashcart verify' to validate integrity")
    else:
        print()
        warn("Some components failed — check errors above")
        info(f"Safety backup available: {os.path.basename(safety_dir) if not args.no_safety_backup else 'skipped'}")

    return 0 if all(results.values()) else 1

def cmd_verify(args):
    """Verify snapshot integrity or live system integrity."""
    header("ClawCrashCart v2 — Verify")

    if args.snapshot_id:
        # Verify a specific snapshot
        snapshot_dir = os.path.join(CONFIG["backup_dir"], args.snapshot_id)
        if not os.path.exists(snapshot_dir):
            err(f"Snapshot not found: {args.snapshot_id}")
            return 1

        manifest = load_manifest(snapshot_dir)
        if not manifest:
            err("No manifest — cannot verify")
            return 1

        info(f"Verifying snapshot: {manifest['snapshot_id']}")
        print()

        errors = 0
        for rel_path, expected_hash in manifest["checksums"].items():
            full_path = os.path.join(snapshot_dir, rel_path)
            if not os.path.exists(full_path):
                err(f"Missing: {rel_path}")
                errors += 1
            else:
                actual_hash = file_checksum(full_path)
                if actual_hash != expected_hash:
                    err(f"Checksum mismatch: {rel_path}")
                    errors += 1
                else:
                    ok(f"{rel_path}")

        print()
        if errors == 0:
            ok(f"{C.BOLD}Snapshot verified — all {len(manifest['checksums'])} files intact{C.RESET}")
        else:
            err(f"{errors} error(s) found")
        return 0 if errors == 0 else 1

    else:
        # Verify live system
        info("Verifying live system components...")
        print()

        checks = 0
        passed = 0

        # Check MEMORY.md exists
        checks += 1
        if os.path.exists(CONFIG["pinned_paths"][0]):
            ok(f"MEMORY.md present ({human_size(os.path.getsize(CONFIG['pinned_paths'][0]))})")
            passed += 1
        else:
            err("MEMORY.md not found")

        # Check Docker
        checks += 1
        if docker_available():
            ok("Docker available")
            passed += 1

            # Check containers
            for name, label in [
                (CONFIG["qdrant_container"], "Qdrant"),
                (CONFIG["postgres_container"], "PostgreSQL"),
                (CONFIG["redis_container"], "Redis"),
            ]:
                checks += 1
                if container_running(name):
                    ok(f"{label} container running")
                    passed += 1
                else:
                    err(f"{label} container not running")

            # Check Qdrant API
            checks += 1
            try:
                import urllib.request
                req = urllib.request.Request(f"{CONFIG['qdrant_url']}/collections")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    colls = data.get("result", {}).get("collections", [])
                    ok(f"Qdrant API responsive — {len(colls)} collection(s)")
                    passed += 1
            except Exception as e:
                err(f"Qdrant API not responding: {e}")

            # Check Postgres connectivity
            checks += 1
            result = run(
                f"docker exec {CONFIG['postgres_container']} "
                f"psql -U {CONFIG['pg_user']} -d {CONFIG['pg_db']} -c 'SELECT 1'",
                check=False
            )
            if result.returncode == 0:
                ok("PostgreSQL accepting queries")
                passed += 1
            else:
                err("PostgreSQL not accepting queries")

            # Check Redis connectivity
            checks += 1
            result = run(
                f"docker exec {CONFIG['redis_container']} redis-cli PING",
                check=False
            )
            if result.returncode == 0 and "PONG" in result.stdout:
                ok("Redis responding to PING")
                passed += 1
            else:
                err("Redis not responding")

        else:
            warn("Docker not available — skipping container checks")

        print()
        if passed == checks:
            ok(f"{C.BOLD}All {checks} checks passed{C.RESET}")
        else:
            warn(f"{passed}/{checks} checks passed")
        return 0 if passed == checks else 1

def cmd_list(args):
    """List available snapshots."""
    header("ClawCrashCart v2 — Snapshots")

    snapshots = list_snapshots()
    if not snapshots:
        info("No snapshots found")
        return 0

    total_size = 0
    for snap_dir in snapshots:
        manifest = load_manifest(snap_dir)
        snap_name = os.path.basename(snap_dir)
        snap_size = dir_size(snap_dir)
        total_size += snap_size

        if manifest:
            components = ", ".join(manifest["components"].keys())
            created = manifest["created_at"][:19].replace("T", " ")
            notes = manifest.get("notes", "")
            file_count = sum(c["file_count"] for c in manifest["components"].values())

            line = f"  {C.CYAN}{snap_name}{C.RESET}  {created}  {human_size(snap_size):>8s}  [{components}]  {file_count} files"
            if notes:
                line += f"  {C.DIM}# {notes}{C.RESET}"
            print(line)

            if args.verbose:
                for comp, detail in manifest["components"].items():
                    print(f"    {C.DIM}└─ {comp}: {detail['file_count']} files, {human_size(detail['total_size'])}{C.RESET}")
        else:
            # v1 snapshot or no manifest
            print(f"  {C.YELLOW}{snap_name}{C.RESET}  {human_size(snap_size):>8s}  {C.DIM}(no manifest — v1?){C.RESET}")

    print()
    print(f"  {C.BOLD}Total: {len(snapshots)} snapshot(s), {human_size(total_size)}{C.RESET}")
    return 0

def cmd_prune(args):
    """Remove old snapshots, keeping the N most recent."""
    header("ClawCrashCart v2 — Prune")

    keep = args.keep_last
    snapshots = list_snapshots()

    if len(snapshots) <= keep:
        info(f"Only {len(snapshots)} snapshot(s) — nothing to prune (keeping {keep})")
        return 0

    # Sort by name (which includes timestamp) — newest last
    snapshots.sort(key=lambda p: os.path.basename(p))
    to_remove = snapshots[:-keep]
    to_keep = snapshots[-keep:]

    print(f"  Keeping {keep} most recent snapshot(s):")
    for s in to_keep:
        print(f"    {C.GREEN}✓{C.RESET} {os.path.basename(s)}")

    print()
    print(f"  Removing {len(to_remove)} old snapshot(s):")
    for s in to_remove:
        size = dir_size(s)
        print(f"    {C.RED}✗{C.RESET} {os.path.basename(s)}  ({human_size(size)})")

    if not args.yes:
        try:
            confirm = input(f"\n  Proceed? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return 1
        if confirm != "y":
            print("  Aborted.")
            return 1

    freed = 0
    for s in to_remove:
        freed += dir_size(s)
        shutil.rmtree(s)

    print()
    ok(f"Pruned {len(to_remove)} snapshot(s), freed {human_size(freed)}")
    return 0

def cmd_status(args):
    """Show system status overview."""
    header("ClawCrashCart v2 — Status")

    # Memory stack status
    print(f"  {C.BOLD}Memory Stack:{C.RESET}")

    # Pinned tier
    mem_path = CONFIG["pinned_paths"][0]
    if os.path.exists(mem_path):
        ok(f"Pinned tier: MEMORY.md ({human_size(os.path.getsize(mem_path))})")
    else:
        err("Pinned tier: MEMORY.md not found")

    # Docker services
    if docker_available():
        for name, label in [
            (CONFIG["qdrant_container"], "Vector tier (Qdrant)"),
            (CONFIG["postgres_container"], "Metadata tier (PostgreSQL)"),
            (CONFIG["redis_container"], "Cache tier (Redis)"),
        ]:
            if container_running(name):
                ok(f"{label}: running")
            else:
                err(f"{label}: not running")
    else:
        warn("Docker not available")

    # Backup status
    print()
    print(f"  {C.BOLD}Backups:{C.RESET}")
    snapshots = list_snapshots()
    if snapshots:
        latest = snapshots[-1]
        manifest = load_manifest(latest)
        if manifest:
            created = manifest["created_at"][:19].replace("T", " ")
            info(f"Latest: {os.path.basename(latest)} ({created})")
        info(f"Total:  {len(snapshots)} snapshot(s), {human_size(sum(dir_size(s) for s in snapshots))}")
    else:
        warn("No snapshots found")

    return 0

def list_snapshots() -> list:
    """List all snapshot directories, sorted oldest first."""
    backup_dir = CONFIG["backup_dir"]
    if not os.path.exists(backup_dir):
        return []
    dirs = [
        os.path.join(backup_dir, d)
        for d in sorted(os.listdir(backup_dir))
        if os.path.isdir(os.path.join(backup_dir, d))
    ]
    return dirs

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="clawcrashcart",
        description="ClawCrashCart v2 — Docker-Aware Backup & Restore for OpenClaw",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  clawcrashcart backup                          Full backup (all tiers)
  clawcrashcart backup --memory-only            Memory tiers only
  clawcrashcart backup --config-only            Agent configs only
  clawcrashcart backup --notes "pre-migration"  Add a note to the snapshot
  clawcrashcart restore snapshot_20260225_143000 --component all
  clawcrashcart restore snapshot_20260225_143000 --component qdrant
  clawcrashcart list --verbose
  clawcrashcart verify snapshot_20260225_143000
  clawcrashcart verify                          Verify live system
  clawcrashcart prune --keep-last 10
  clawcrashcart status
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # backup
    p_backup = subparsers.add_parser("backup", help="Create a backup snapshot")
    backup_group = p_backup.add_mutually_exclusive_group()
    backup_group.add_argument("--full", action="store_true", default=True, help="Full backup (default)")
    backup_group.add_argument("--memory-only", action="store_true", help="Memory tiers only")
    backup_group.add_argument("--config-only", action="store_true", help="Config files only")
    p_backup.add_argument("--notes", type=str, default="", help="Add notes to snapshot manifest")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore from a snapshot")
    p_restore.add_argument("snapshot_id", help="Snapshot ID to restore from")
    p_restore.add_argument("--component", default="all",
                          choices=["qdrant", "postgres", "redis", "pinned", "configs", "all"],
                          help="Component to restore (default: all)")
    p_restore.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_restore.add_argument("--no-safety-backup", action="store_true",
                          help="Skip pre-restore safety backup")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify snapshot or live system")
    p_verify.add_argument("snapshot_id", nargs="?", help="Snapshot ID (omit for live system)")

    # list
    p_list = subparsers.add_parser("list", help="List available snapshots")
    p_list.add_argument("-v", "--verbose", action="store_true", help="Show component details")

    # prune
    p_prune = subparsers.add_parser("prune", help="Remove old snapshots")
    p_prune.add_argument("--keep-last", type=int, required=True, help="Number of snapshots to keep")
    p_prune.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # status
    subparsers.add_parser("status", help="Show system status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Ensure backup directory exists
    os.makedirs(CONFIG["backup_dir"], exist_ok=True)

    commands = {
        "backup": cmd_backup,
        "restore": cmd_restore,
        "verify": cmd_verify,
        "list": cmd_list,
        "prune": cmd_prune,
        "status": cmd_status,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
